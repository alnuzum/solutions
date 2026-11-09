"""Price rules: every price comes from the catalog through the company's rules, and rules out of bounds
are refused, never clipped."""

from decimal import Decimal

import pytest

from alnuzum_orders.pricing import Rules, RulesError, Tier, demo_rules, money, parse_rules, price

D = Decimal


def test_category_rule_wins_over_the_default_and_matches_any_spelling() -> None:
    rules = parse_rules({"default": "2", "categories": [{"name": "Кабель и провод", "percent": "5"}]})
    assert rules.percent_for("кабель  И ПРОВОД") == D(5)
    assert rules.percent_for("Крепёж") == D(2)
    assert rules.percent_for(None) == D(2)


def test_price_of_lines_category_then_volume_then_rounding() -> None:
    rules = Rules(
        default=D(0),
        categories={"Кабель": D(5), "Инструмент": D(-10)},
        volume=(Tier(D(1000), D(3)), Tier(D(5000), D(5))),
        round=D("0.01"),
    )
    p = price(
        [(1, D("100"), "Кабель", D(10)), (2, D("333.33"), "Инструмент", D(3)), (3, D(50), None, D(1))], rules
    )
    # 105 x 10 + 300 x 3 + 50 = 2000: the 1000 tier (3 %) applies, not the 5000 one
    assert p.subtotal == D("2000.00")
    assert p.tier == Tier(D(1000), D(3))
    assert [ln.unit_price for ln in p.lines] == [D("101.85"), D("291.00"), D("48.50")]
    assert p.total == D("101.85") * 10 + D("291.00") * 3 + D("48.50")
    assert p.base_total == D("100") * 10 + D("333.33") * 3 + D(50)
    assert [ln.percent for ln in p.lines] == [D(5), D(-10), D(0)]


@pytest.mark.parametrize(
    ("subtotal", "tier"),
    [(D("999.99"), None), (D(1000), D(3)), (D("4999.99"), D(3)), (D(5000), D(5)), (D(10**9), D(5))],
)
def test_volume_tier_boundaries(subtotal: Decimal, tier: Decimal | None) -> None:
    rules = Rules(volume=(Tier(D(1000), D(3)), Tier(D(5000), D(5))))
    found = rules.tier_for(subtotal)
    assert (found.percent if found else None) == tier


@pytest.mark.parametrize(
    ("step", "value", "out"),
    [("0.01", "10.005", "10.01"), ("1", "10.5", "11"), ("10", "14.9", "10"), ("100", "150", "200")],
)
def test_rounding_steps(step: str, value: str, out: str) -> None:
    assert money(D(value), D(step)) == D(out)


@pytest.mark.parametrize(
    ("data", "code"),
    [
        ({"categories": [{"name": "A", "percent": "301"}]}, "e_rules_percent"),
        ({"categories": [{"name": "A", "percent": "-91"}]}, "e_rules_percent"),
        ({"default": "1000"}, "e_rules_percent"),
        ({"volume": [{"from": "100", "percent": "91"}]}, "e_rules_percent"),
        ({"volume": [{"from": "100", "percent": "-1"}]}, "e_rules_percent"),
        ({"volume": [{"from": "-5", "percent": "1"}]}, "e_rules_percent"),
        ({"categories": [{"name": "A", "percent": "abc"}]}, "e_rules_number"),
        ({"categories": [{"name": "A", "percent": "NaN"}]}, "e_rules_number"),
        ({"categories": [{"name": "A", "percent": "Infinity"}]}, "e_rules_number"),
        ({"categories": [{"name": f"c{i}", "percent": "1"} for i in range(51)]}, "e_rules_many"),
        ({"volume": [{"from": str(i), "percent": "1"} for i in range(6)]}, "e_rules_many"),
        ({"round": "0.5"}, "e_rules_round"),
    ],
)
def test_rules_out_of_bounds_are_refused(data: dict[str, object], code: str) -> None:
    with pytest.raises(RulesError) as caught:
        parse_rules(data)
    assert caught.value.code == code


def test_rules_at_the_bounds_are_kept_and_survive_json() -> None:
    data = {
        "default": "-90",
        "categories": [
            {"name": "A", "percent": "300"},
            {"name": "", "percent": "5"},
            {"name": "B", "percent": ""},
        ],
        "volume": [{"from": "1 000,5", "percent": "90"}, {"from": "", "percent": "3"}],
        "round": "100",
    }
    rules = parse_rules(data)
    assert rules.default == D(-90)
    assert rules.categories == {"A": D(300)}
    assert rules.volume == (Tier(D("1000.5"), D(90)),)
    assert parse_rules(rules.to_json()) == rules
    assert parse_rules(None) == Rules()


def test_demo_rules_follow_the_account_currency() -> None:
    uzs, rub, usd = demo_rules("UZS"), demo_rules(None), demo_rules("usd")
    assert uzs.round == D(100) and rub.round == D("0.01")
    assert uzs.volume[0].start == D(30000) * 155
    assert usd.volume[0].start == D(30000) * D("0.0125")
    assert rub.percent_for("Кабель и провод") == D(5)


# --- I7: rounding never makes a price nothing ---


def test_a_price_never_rounds_to_zero_or_below() -> None:
    from alnuzum_orders.pricing import money

    assert money(D(30), D(100)) == D(30)  # under half a step: kept to the cent
    assert money(D(49.9), D(100)) == D("49.90")
    assert money(D(60), D(100)) == D(100)
    assert money(D("0.004"), D("0.01")) == D("0.01")
    assert money(D(0), D(100)) == D(0)
    rules = Rules(default=D(-90), volume=(Tier(D(0), D(90)),), round=D("0.01"))
    p = price([(1, D("0.4"), None, D(1)), (2, D(499), None, D(2))], rules)
    assert all(ln.unit_price > 0 for ln in p.lines) and p.total > 0
    hundred = price([(1, D(30), None, D(3)), (2, D(499), None, D(1))], Rules(default=D(-90), round=D(100)))
    assert [ln.unit_price for ln in hundred.lines] == [D(3), D("49.90")]
