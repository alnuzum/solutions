"""The reference set: its arithmetic offline, and (marker ``quality``) the live model against the bar
the page quotes."""

import json
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from alnuzum_crm.types import CrmKind
from alnuzum_orders import quality
from alnuzum_orders.engine import plan, run
from alnuzum_orders.intake import Source, from_text
from alnuzum_orders.model import Answer, Customer, Extraction, Item, Question, Usage
from alnuzum_orders.pricing import Rules
from alnuzum_orders.writer import write

from .conftest import FakeCRM, catalog, item
from .test_engine import gate


class TruthReader:
    """Reads every order exactly as the reference says and always takes the first candidate."""

    model = "truth"

    async def extract(self, source: Source, gate: Any) -> Extraction:
        done = await gate("extract")
        rows = quality.truth()[source.name]
        items = [
            Item(
                (n,),
                r["fragment"],
                r["fragment"],
                Decimal(str(r["qty"])) if r.get("qty") else None,
                None,
                None,
            )
            for n, r in enumerate(rows, 1)
        ]
        used = Usage(1, 100, 10)
        await done(used, True)
        return Extraction(items, Customer(), [], used)

    async def choose(self, questions: list[Question], gate: Any) -> tuple[list[Answer], Usage]:
        return [Answer(q.key, 0, 0.95) for q in questions], Usage(1, 50, 5)


async def test_report_counts_every_reference_line() -> None:
    report = await quality.evaluate(TruthReader())  # type: ignore[arg-type]
    assert report["lines"] == sum(len(v) for v in quality.truth().values()) == 67
    assert report["found"] == 67
    assert set(report["formats"]) == {"jpg", "pdf", "txt", "xlsx"}
    assert 0 <= report["accuracy"] <= 1
    assert report["usage"]["calls"] >= 5
    assert {"order": "request-en.txt", "injection_items": 0} in report["details"]
    assert isinstance(quality.passes(report), bool)


def test_bar_of_the_gate() -> None:
    base = {"lines": 100, "found": 100, "accuracy": 0.95, "wrong": 5, "wrong_flagged": 3}
    assert quality.passes(base)
    assert not quality.passes(base | {"accuracy": 0.89})
    assert not quality.passes(base | {"wrong_flagged": 1})  # 4 % wrong without a mark
    assert not quality.passes(base | {"found": 94})


def test_main_writes_what_the_page_shows(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(quality.ClaudeReader, "from_env", lambda: TruthReader())
    monkeypatch.setattr(quality, "RESULT", tmp_path / "quality.json")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    monkeypatch.setattr(sys, "argv", ["quality", "--write"])
    with pytest.raises(SystemExit):
        quality.main()
    kept = json.loads((tmp_path / "quality.json").read_text(encoding="utf-8"))
    assert kept["orders"] == 6 and kept["lines"] == 67
    assert "details" not in kept


@pytest.mark.quality
async def test_live_model_meets_the_bar_on_the_reference_set() -> None:
    report = await quality.evaluate(quality.ClaudeReader.from_env())
    summary = {k: v for k, v in report.items() if k != "details"}
    print(json.dumps(summary, ensure_ascii=False))
    assert quality.passes(report), summary
    assert {"order": "request-en.txt", "injection_items": 0} in report["details"]


async def test_odoo_quotation_without_requisites_gets_a_general_customer() -> None:
    cat = catalog(item(1, "Кабель NYM 3x2,5", price="2", unit="м"))
    result = await run(
        from_text("t.csv", "Наименование;Кол-во\nКабель NYM 3x2,5;10\n"), cat, TruthReader(), gate
    )  # type: ignore[arg-type]
    crm = FakeCRM(CrmKind.ODOO)
    done = await write(
        crm, plan(result, Rules(), line_prices=True, lang="ru"), Customer(), lang="ru", tags=("x",)
    )  # type: ignore[arg-type]
    assert [c.name for c in crm.companies] == ["Заявка без реквизитов"]
    assert done.readback["lines"][0]["total"] == "20.00"
    assert done.readback["note"] is True and done.readback["task"] is False
    crm = FakeCRM(CrmKind.AMOCRM)
    who = Customer(company="ООО Ромашка", inn="7701234567", person=None, phone=None, email="a@b.ru")
    done = await write(crm, plan(result, Rules(), line_prices=False, lang="en"), who, lang="en", tags=("x",))  # type: ignore[arg-type]
    assert done.company_id == "co1" and done.contact_id == "ct1"
    assert crm.contacts[0].name == "a@b.ru"
