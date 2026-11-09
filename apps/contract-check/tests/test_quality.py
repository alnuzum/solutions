"""The reference set: the comparison alone (a reader that rates nothing and reads a scan as printed) finds every
change made and no other; the live model is held to the bar of CI."""

import os
from decimal import Decimal
from typing import Any

import pytest
from alnuzum_contracts import quality
from alnuzum_contracts.clauses import Comparison
from alnuzum_contracts.model import apply_rating

from alnuzum_orders.model import Usage


class AsPrinted:
    """No model: every change medium, a scan read exactly as it was printed."""

    async def read_scan(self, pictures: list[bytes], gate: Any) -> tuple[list[str], Usage]:
        pair = next(p for p in quality.truth()["pairs"] if p["format"] == "scan")
        return list(pair["theirs"]), Usage()

    async def rate(self, cmp: Comparison, *, side: str, lang: str, gate: Any) -> Any:
        answer = {
            "changes": [
                {"id": c.id, "topic": "other", "risk": "medium", "substantive": True, "why": ""}
                for c in cmp.changes
            ]
        }
        apply_rating(cmp.changes, answer, {c.id for c in cmp.changes})


async def test_the_comparison_alone_finds_every_change_made_and_no_other() -> None:
    result = await quality.measure(AsPrinted(), None)
    assert result["changes"] >= 40 and result["pairs"] == 4
    misses = {
        r["id"]: (r["missed"], r["false_list"]) for r in result["rows"] if r["missed"] or r["false_list"]
    }
    assert misses == {}
    assert result["found"] == result["changes"] and result["false"] == 0
    assert result["quotes"] == 1.0 and result["quotes_scan"] == 1.0
    kinds = {r["id"]: r["changes"] for r in result["rows"]}
    assert set(kinds) == {"supply-ru-docx", "services-ru-pdf", "supply-en-txt", "supply-ru-scan"}


def test_the_bar() -> None:
    good = {"changes": 44, "found": 43, "false": 1, "risk_match": 36, "quotes": 1.0, "cosmetic_high": 0}
    assert quality.check(good) == []
    assert quality.check(good | {"cosmetic_high": 1, "quotes": 0.98})


@pytest.mark.quality
async def test_the_live_model_on_the_reference_set() -> None:
    if not os.environ.get("ANTHROPIC_OAUTH_TOKEN") and not os.environ.get("ANTHROPIC_API_KEY"):
        pytest.fail("ANTHROPIC_OAUTH_TOKEN or ANTHROPIC_API_KEY is needed")
    result = await quality.run(write=False)
    print({k: v for k, v in result.items() if k != "rows"})
    for row in result["rows"]:
        print(row["id"], row["missed"], row["false_list"])
    assert quality.check(result) == [], result
    assert Decimal(result["usd"]) < Decimal("1.5")
