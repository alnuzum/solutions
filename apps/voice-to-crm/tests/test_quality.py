"""The reference set: its arithmetic offline, and (marker ``quality``) the live speech service and model
against the bar of CI."""

import json
import os
import sys
from datetime import date, time
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from alnuzum_voice import quality
from alnuzum_voice.model import (
    AmountAsk,
    DateAsk,
    Intent,
    ProductAsk,
    Refusal,
    Spent,
    StageAsk,
    TaskAsk,
    Transcript,
)

from alnuzum_orders.model import Answer, Question, Usage
from alnuzum_orders.quality import demo_catalog

from .conftest import Harness


class TruthScribe:
    """Hears every command exactly as it was said."""

    def __init__(self, only: list[str] | None = None) -> None:
        self.texts = [c["text"] for c in quality.samples()["commands"] if not only or c["id"] in only]
        self.calls = 0

    async def transcribe(self, wav: bytes, seconds: int, gate: Any) -> Transcript:
        done = await gate("stt", Decimal("0.01"), seconds)  # as Scribe asks: the seconds are booked too
        text = self.texts[self.calls]
        self.calls += 1
        await done(Spent(Decimal("0.0005"), seconds=seconds), True)
        return Transcript(text, None)


# words that two catalog items fit, as the open products of the set say them
OPEN = {
    "EL-KL": ("клемма WAGO на 2 провода", Decimal(2), "уп"),
    "EL-SH": ("щиток на 12 модулей", Decimal(4), None),
}


def _at(task: dict[str, Any]) -> time | None:
    return time.fromisoformat(task["time"]) if task.get("time") else None


class TruthReader:
    """Reads every command as the reference says; an open product is said as words two items fit."""

    model = "truth"

    def __init__(self) -> None:
        self.commands = {c["text"]: c for c in quality.samples()["commands"]}
        self.names = {i.sku: i.name for i in demo_catalog().items}
        stages = quality.samples()["stages"]
        self.stage_ids = {s["name"]: s["id"] for s in stages}

    async def intent(self, phrases: list[str], stages: Any, today: date, gate: Any) -> Intent:
        done = await gate("intent", Decimal("0.02"))
        cmd = self.commands[" ".join(phrases)]
        deal = quality.samples()["deals"][cmd["deal"]]
        task = cmd.get("task")
        out = Intent(
            [deal["company"]],
            (1,),
            stage=StageAsk((1,), self.stage_ids[cmd["stage"]]) if "stage" in cmd else None,
            amount=AmountAsk((1,), Decimal(str(cmd["amount"]))) if "amount" in cmd else None,
            close_date=DateAsk((1,), date.fromisoformat(cmd["close_date"])) if "close_date" in cmd else None,
            products=[
                ProductAsk((1,), self.names[sku], Decimal(str(q)), None) for sku, q in cmd.get("products", [])
            ]
            + [ProductAsk((1,), *OPEN[skus[0][:5]]) for skus in cmd.get("unsure", [])],
            task=TaskAsk((1,), task["kind"], date.fromisoformat(task["due"]), _at(task)) if task else None,
            refused=[Refusal((1,), w) for w in cmd.get("refused", [])],
        )
        out.usage = Usage(1, 100, 10)
        await done(out.usage, True)
        return out

    async def choose(self, questions: list[Question], gate: Any) -> tuple[list[Answer], Usage]:
        return [Answer(q.key, None, 0.9) for q in questions], Usage(1, 50, 5)


def test_words_fold_case_alphabets_and_numbers() -> None:
    assert quality.words("Три на два с половиной") == ["#"]
    assert quality.words("3x2,5") == ["#"]
    assert quality.words("пять миллионов двести тысяч") == quality.words("5 200 000") == ["#"]
    assert quality.words("Ертага соат ўнда учрашув.") == quality.words("Ertaga soat o'nda uchrashuv")
    assert quality.words("uchrashuv")[0] == "uchrashuv"  # «uch» (three) only as a word of its own
    assert quality.errors("Навруз Строй, этап", "НаврузСтрой. Этап") == (2, 3)


async def test_report_counts_every_reference_command() -> None:
    report = await quality.evaluate(TruthScribe(), TruthReader())
    assert report["commands"] == 22 and report["uzbek"] == 10
    assert report["wer"] == 0.0 and report["deal"] == 1.0 and report["extra"] == 0
    wrong = [r for r in report["details"] if not r["exact"]]
    assert wrong == [], wrong
    assert report["usage"]["seconds"] >= 22 and report["usage"]["calls"] >= 44
    assert quality.passes(report)


async def test_a_plan_is_scored_change_by_change() -> None:
    only = ["ru-01", "ru-09"]
    report = await quality.evaluate(TruthScribe(only), _Off(), only=only)
    rows = {r["id"]: r for r in report["details"]}
    assert not rows["ru-01"]["exact"] and rows["ru-01"]["extra"] == 1  # a stage nobody asked for
    assert "('stage', '1:20')" in rows["ru-01"]["missing"]
    assert not rows["ru-09"]["exact"] and "refused price" in rows["ru-09"]["missing"]


class _Off(TruthReader):
    """Takes the wrong stage and forgets a refusal."""

    async def intent(self, phrases: list[str], stages: Any, today: date, gate: Any) -> Intent:
        out = await super().intent(phrases, stages, today, gate)
        out.stage = StageAsk((1,), "1:40")
        out.refused = [r for r in out.refused if r.what != "price"]
        return out


def test_bar_of_the_gate() -> None:
    base = {"wer": 0.1, "deal": 0.95, "exact": 0.86, "extra": 1}
    assert quality.passes(base)
    assert not quality.passes(base | {"wer": 0.16})
    assert not quality.passes(base | {"deal": 0.86})
    assert not quality.passes(base | {"exact": 0.77})
    assert not quality.passes(base | {"extra": 3})


def test_main_writes_what_the_page_shows(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(quality, "Scribe", lambda key: TruthScribe())
    monkeypatch.setattr(quality.VoiceReader, "from_env", lambda: TruthReader())
    monkeypatch.setattr(quality, "RESULT", tmp_path / "quality.json")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    monkeypatch.setenv("ELEVENLABS_API_KEY", "x")
    monkeypatch.setattr(sys, "argv", ["quality", "--write"])
    with pytest.raises(SystemExit) as exit_:
        quality.main()
    assert exit_.value.code == 0
    kept = json.loads((tmp_path / "quality.json").read_text(encoding="utf-8"))
    assert set(kept) == set(quality.KEEP) and kept["commands"] == 22
    assert "details" not in kept


async def test_the_page_quotes_the_set_as_synthesised(make: Any) -> None:
    kept = json.loads(quality.RESULT.read_text(encoding="utf-8"))
    h: Harness = await make()
    async with h.client() as c:
        ru, en = (await c.get("/ru/voice")).text, (await c.get("/en/voice")).text
    assert f"Эталон: {kept['commands']} команды, синтезированные голоса" in ru
    assert "точность на живых голосовых с шумом эталон не обещает" in ru
    assert f"{kept['commands']} commands, synthesised voices" in en


@pytest.mark.quality
async def test_live_speech_and_model_meet_the_bar_on_the_reference_set() -> None:
    report = await quality.evaluate(
        quality.Scribe(os.environ["ELEVENLABS_API_KEY"]), quality.VoiceReader.from_env()
    )
    summary = {k: v for k, v in report.items() if k != "details"}
    print(json.dumps(summary, ensure_ascii=False))
    for row in report["details"]:
        if not row["exact"] or row["extra"]:
            print("WRONG", json.dumps({k: v for k, v in row.items() if k != "heard"}, ensure_ascii=False))
    assert quality.passes(report), summary


async def test_a_joined_or_misheard_name_still_finds_its_deal_and_only_a_demo_one() -> None:
    from alnuzum_voice.engine import find_candidates, pick

    crm = quality.ReferenceCRM(quality.samples())
    for said in (["НаврузСтрой"], ["Дилшот Расулов"]):
        found = await find_candidates(crm, said, demo=True)  # type: ignore[arg-type]
        assert [c["id"] for c in found] == ["D2"] and pick(found, said) == "D2"
    found = await find_candidates(crm, ["Ромашка"], demo=True)  # type: ignore[arg-type]
    assert [c["id"] for c in found] == ["D1"]  # the deal outside the demo is not offered
    assert await find_candidates(crm, ["Кто-то Неизвестный"], demo=True) == []  # type: ignore[arg-type]
