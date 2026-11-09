"""The reference set: a reader that answers exactly what each letter states keeps every value (each is written in
its letter) and our matching picks the company of the truth; the live model is held to the bar of CI."""

import json
import os
from decimal import Decimal
from importlib import resources
from typing import Any

import pytest
from alnuzum_mail import quality
from alnuzum_mail.intake import Letter
from alnuzum_mail.model import FIELDS, Reading, apply_reading


class AsWritten:
    """No model: the class and the requisites of the truth, each quoted as its value."""

    def __init__(self) -> None:
        self.truth = {item["text"]: item["expect"] for item in quality.truth()["letters"]}

    async def triage(self, letter: Letter, *, lang: str, gate: Any) -> Reading:
        want = next(v for k, v in self.truth.items() if letter.subject in k and letter.body[:40] in k)
        fields = {k: {"value": want["fields"].get(k), "quote": want["fields"].get(k)} for k in FIELDS}
        data = {
            "class": want["cls"],
            "language": "ru",
            "request": "",
            "why": "",
            "fields": fields,
            "scans": [],
        }
        return apply_reading(data, letter)


async def test_the_set_is_consistent_with_its_letters() -> None:
    result = await quality.measure(AsWritten(), None)
    assert result["letters"] >= 25 and result["langs"] == ["en", "ru", "uz"]
    lost = {r["id"]: (r["wrong"], r["chosen"]) for r in result["rows"] if r["wrong"] or not r["cls_ok"]}
    assert lost == {}
    assert result["fields_ok"] == result["fields"] and result["cls"] == result["letters"]
    unbound = {r["id"]: r["chosen"] for r in result["rows"] if r["bind"] and not r["bound"]}
    assert unbound == {}
    assert result["injections"] >= 4 and result["injection"] == result["injections"]
    kinds = {r["cls"] for r in result["rows"]}
    assert kinds == {"new", "reply", "junk"}


def test_the_bar() -> None:
    good = {"letters": 31, "cls": 29, "fields": 120, "fields_ok": 112, "wrong": 3, "bind": 25, "bound": 23,
            "injections": 4, "injection": 4}  # fmt: skip
    assert quality.check(good) == []
    assert (
        len(quality.check(good | {"cls": 20, "fields_ok": 90, "wrong": 9, "bound": 10, "injection": 3})) == 5
    )


def test_the_page_numbers_are_those_the_page_shows() -> None:
    kept = json.loads(resources.files("alnuzum_mail").joinpath("quality.json").read_text())
    assert set(kept["shown"]) == set(quality.SHOWN)
    assert kept["shown"]["letters"] == len(quality.truth()["letters"])


@pytest.mark.quality
async def test_the_live_model_on_the_reference_set() -> None:
    if not os.environ.get("ANTHROPIC_OAUTH_TOKEN") and not os.environ.get("ANTHROPIC_API_KEY"):
        pytest.fail("ANTHROPIC_OAUTH_TOKEN or ANTHROPIC_API_KEY is needed")
    result = await quality.run(write=False)
    print({k: v for k, v in result.items() if k != "rows"})
    for row in result["rows"]:
        if not row["cls_ok"] or row["wrong"] or (row["bind"] and not row["bound"]):
            print(row["id"], row["cls"], row["wrong"], row["chosen"])
    assert quality.check(result) == [], result
    assert Decimal(result["usd"]) < Decimal("2")
