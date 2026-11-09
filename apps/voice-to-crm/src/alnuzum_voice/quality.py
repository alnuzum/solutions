"""The reference set of the voice solution: 22 spoken commands (12 Russian, 10 Uzbek) about five demo deals.

    ELEVENLABS_API_KEY=… ANTHROPIC_API_KEY=… uv run python -m alnuzum_voice.quality [--write]

The audio is synthesised (ElevenLabs, five voices, clean speech): the numbers say how the chain reads a
clear command, not how it hears a street. Each command goes the way a voice note goes: decoded, heard by
the speech service, cut into phrases, read by the model, matched to a deal and to the catalog.

Metrics:
- wer: word errors of the speech against what was said, over all words of the set; words are compared
  folded: case and punctuation dropped, Uzbek Cyrillic read as Latin, and a run of numbers (digits or
  number words, «три на два с половиной» or «3x2,5») counts as one word;
- deal: the command's deal was taken by the system alone;
- exact: the deal is right and the plan's changes are exactly the expected ones: stage, sum, closing
  date, products by article and quantity, task kind and date (and time when one was said), every expected
  refusal; a product the words leave open (``unsure`` in the set) is right when the plan leaves it to a
  person with the expected items among the candidates;
- extra: changes the plan would write that the command did not ask for, over the whole set.
The CRM is a stand-in with the five demo deals and one more deal of the same name outside the demo; the
catalog is the demo catalog of solution 1.
"""

import argparse
import asyncio
import json
import os
import re
import unicodedata
from datetime import UTC, date, datetime
from decimal import Decimal
from importlib import resources
from pathlib import Path
from typing import Any, ClassVar

from alnuzum_crm.types import DealHit, DealStatus, Stage
from alnuzum_orders.quality import demo_catalog
from alnuzum_voice import audio
from alnuzum_voice.engine import Result, analyze
from alnuzum_voice.model import STT_MODEL, Scribe, VoiceReader
from alnuzum_voice.text import phrases, to_latin

# the gate of CI: below these the change does not go in
MAX_WER = 0.15
MIN_DEAL = 0.9
MIN_EXACT = 0.8
MAX_EXTRA = 2
RESULT = Path(__file__).with_name("quality.json")
DECOY = "Ромашка: чужая сделка"

_KIND = {"open": DealStatus.OPEN, "won": DealStatus.WON, "lost": DealStatus.LOST}
# number words, Latin-folded (Russian through the same table as Uzbek Cyrillic)
_NUMBER = re.compile(
    r"(?:\d+(?:[.,]\d+)?"
    r"|nol|nul|odin|odna|odno|odnogo|dva|dve|dvuh|dvux|tri|treh|trex|chetire|chetireh|chetirex|pyat|shest|sem|"
    r"vosem|devyat|desyat|\w+nadtsat\w*|dvadtsat\w*|tridtsat\w*|sorok\w*|pyatdesyat\w*|shestdesyat\w*|"
    r"semdesyat\w*|vosemdesyat\w*|devyanosto\w*|sto|sta|dvesti|trista|chetiresta|\w+sot|tisyach\w*|"
    r"million\w*|milliard\w*|polovin\w*|poltora|"
    r"(?:bir|ikki|uch|to'rt|besh|olti|yetti|sakkiz|to'qqiz|o'n|yigirma|o'ttiz|qirq|ellik|oltmish|yetmish|"
    r"sakson|to'qson|yuz|ming|yarim)(?:ta|nchi|da|ga|dan|ni|i)?)$"
)
_JOINS = {"s", "na", "x", "i"}  # «два с половиной», «три на два»: between two numbers, part of the run


def samples() -> dict[str, Any]:
    root = resources.files("alnuzum_voice") / "samples"
    return json.loads((root / "truth.json").read_text(encoding="utf-8"))  # type: ignore[no-any-return]


def audio_of(command_id: str) -> bytes:
    return (resources.files("alnuzum_voice") / "samples" / f"{command_id}.ogg").read_bytes()


def words(text: str) -> list[str]:
    """Folded words; a run of numbers is one ``#``."""
    value = unicodedata.normalize("NFKC", text).lower().replace("ё", "е")
    value = re.sub(r"(\d)\s*[x×х*]\s*(\d)", r"\1 \2", value)
    raw = re.sub(r"[^\w']+", " ", to_latin(value).replace("''", "'")).split()
    out: list[str] = []
    for n, word in enumerate(raw):
        is_number = bool(_NUMBER.match(word))
        joins = word in _JOINS and out[-1:] == ["#"] and n + 1 < len(raw) and bool(_NUMBER.match(raw[n + 1]))
        if is_number or joins:
            if out[-1:] != ["#"]:
                out.append("#")
        else:
            out.append(word)
    return out


def errors(said: str, heard: str) -> tuple[int, int]:
    """(word errors, words said): the edit distance over folded words."""
    ref, hyp = words(said), words(heard)
    row = list(range(len(hyp) + 1))
    for i, r in enumerate(ref, 1):
        prev, row[0] = row[0], i
        for j, h in enumerate(hyp, 1):
            prev, row[j] = row[j], min(row[j] + 1, row[j - 1] + 1, prev + (r != h))
    return row[-1], len(ref)


class ReferenceCRM:
    """The voice side of a CRM holding the demo deals of the set (ids ``D1``…) and a decoy outside it."""

    kind = "reference"
    capabilities: ClassVar[frozenset[str]] = frozenset({"close_date", "line_prices"})

    def __init__(self, truth: dict[str, Any]) -> None:
        self._stages = [Stage(s["id"], s["name"], _KIND[s["kind"]], "Продажи") for s in truth["stages"]]
        first = self._stages[0].id
        self.deals = [
            DealHit(key, d["title"], d["company"], d["contact"], first, Decimal(0), DealStatus.OPEN)
            for key, d in truth["deals"].items()
        ]
        self.deals.append(DealHit("X1", DECOY, "Ромашка", None, first, Decimal(0), DealStatus.OPEN))

    async def stages(self) -> list[Stage]:
        return self._stages

    async def find_deals(self, query: str, *, limit: int = 8) -> list[DealHit]:
        q = query.casefold().strip()
        found = []
        for d in self.deals:
            for by, value in (("title", d.title), ("company", d.company), ("contact", d.contact)):
                if q and q in (value or "").casefold():
                    found.append(
                        DealHit(d.id, d.title, d.company, d.contact, d.stage_id, d.amount, d.status, by)
                    )
                    break
        return found[:limit]


def expected(cmd: dict[str, Any], stage_ids: dict[str, str]) -> set[tuple[Any, ...]]:
    out: set[tuple[Any, ...]] = set()
    if "stage" in cmd:
        out.add(("stage", stage_ids[cmd["stage"]]))
    if "amount" in cmd:
        out.add(("amount", Decimal(str(cmd["amount"]))))
    if "close_date" in cmd:
        out.add(("close_date", cmd["close_date"]))
    for sku, qty in cmd.get("products", []):
        out.add(("product", sku, Decimal(str(qty)).normalize()))
    if task := cmd.get("task"):
        out.add(("task", task["kind"], task["due"], task.get("time")))
    for skus in cmd.get("unsure", []):
        out.add(("unsure", *sorted(skus)))
    return out


def planned(result: Result, cmd: dict[str, Any]) -> set[tuple[Any, ...]]:
    """What the plan would write, and the product lines it leaves to a person (with their candidates)."""
    out: set[tuple[Any, ...]] = set()
    said_time = bool((cmd.get("task") or {}).get("time"))
    for c in result.changes:
        if c.kind == "product" and c.state == "unsure":
            out.add(("unsure", *sorted(str(x["sku"]) for x in c.candidates)))
        elif c.state != "ok" or c.kind == "refused":
            continue
        elif c.kind == "stage":
            out.add(("stage", c.stage_id))
        elif c.kind == "amount":
            out.add(("amount", Decimal(str(c.value))))
        elif c.kind == "close_date":
            out.add(("close_date", c.value))
        elif c.kind == "product":
            out.add(("product", c.sku, Decimal(str(c.quantity)).normalize()))
        elif c.kind == "task":
            out.add(("task", c.task_kind, c.due, c.at if said_time else None))
    return out


def _covers(got: tuple[Any, ...], want: tuple[Any, ...]) -> bool:
    """A line left to a person is right when the expected products are among its candidates."""
    if want[0] == "unsure":
        return got[0] == "unsure" and set(want[1:]) <= set(got[1:])
    return got == want


def score(cmd: dict[str, Any], heard: str, result: Result, stage_ids: dict[str, str]) -> dict[str, Any]:
    want = expected(cmd, stage_ids)
    got = planned(result, cmd)
    refused = {c.what for c in result.changes if c.kind == "refused"}
    missing = [w for w in want if not any(_covers(g, w) for g in got)]
    unexpected = [g for g in got if not any(_covers(g, w) for w in want)]
    # a refusal writes nothing: one more than expected is not a mistake, a missing one is
    missing_refusals = sorted(set(cmd.get("refused", [])) - refused)
    wrong, count = errors(cmd["text"], heard)
    right_deal = result.deal_id == cmd["deal"]
    return {
        "id": cmd["id"],
        "lang": cmd["lang"],
        "word_errors": wrong,
        "words": count,
        "deal": right_deal,
        "exact": right_deal and not missing and not unexpected and not missing_refusals,
        "extra": sum(1 for g in unexpected if g[0] != "unsure"),
        "missing": sorted(str(t) for t in missing) + [f"refused {r}" for r in missing_refusals],
        "unexpected": sorted(str(t) for t in unexpected),
        "refused": sorted(r for r in refused if r),
        "heard": heard,
    }


async def evaluate(scribe: Any, reader: Any, *, only: list[str] | None = None) -> dict[str, Any]:
    truth = samples()
    today = date.fromisoformat(truth["today"])
    crm = ReferenceCRM(truth)
    stage_ids = {s["name"]: s["id"] for s in truth["stages"]}
    catalog = demo_catalog()
    usd, calls, seconds = Decimal(0), 0, 0

    async def gate(kind: str, worst: Any = None):  # type: ignore[no-untyped-def]
        async def done(used: Any, ok: bool) -> None:
            nonlocal usd, calls
            if used is not None:
                usd += used.usd
                calls += 1

        return done

    details = []
    for cmd in truth["commands"]:
        if only and cmd["id"] not in only:
            continue
        sound = await asyncio.to_thread(audio.normalize, audio_of(cmd["id"]))
        seconds += sound.seconds
        heard = await scribe.transcribe(sound.wav, sound.seconds, gate)
        result = await analyze(
            phrases=phrases(heard.text),
            language=heard.language,
            source="voice",
            crm=crm,  # type: ignore[arg-type]
            catalog=catalog,
            reader=reader,
            gate=gate,
            demo=True,
            today=today,
        )
        details.append(score(cmd, heard.text, result, stage_ids))
    n = len(details)

    def wer(rows: list[dict[str, Any]]) -> float:
        said = sum(r["words"] for r in rows)
        return round(sum(r["word_errors"] for r in rows) / said, 4) if said else 0.0

    return {
        "date": datetime.now(UTC).strftime("%Y-%m-%d"),
        "model": getattr(reader, "model", "?"),
        "speech": STT_MODEL,
        "commands": n,
        "uzbek": sum(1 for r in details if r["lang"] == "uz"),
        "wer": wer(details),
        "wer_ru": wer([r for r in details if r["lang"] == "ru"]),
        "wer_uz": wer([r for r in details if r["lang"] == "uz"]),
        "deal": round(sum(r["deal"] for r in details) / n, 4) if n else 0.0,
        "exact": round(sum(r["exact"] for r in details) / n, 4) if n else 0.0,
        "extra": sum(r["extra"] for r in details),
        "usage": {"calls": calls, "seconds": seconds, "usd": str(usd.quantize(Decimal("0.0001")))},
        "details": details,
    }


def passes(report: dict[str, Any]) -> bool:
    return bool(
        report["wer"] <= MAX_WER
        and report["deal"] >= MIN_DEAL
        and report["exact"] >= MIN_EXACT
        and report["extra"] <= MAX_EXTRA
    )


KEEP = (
    "date",
    "model",
    "speech",
    "commands",
    "uzbek",
    "wer",
    "wer_ru",
    "wer_uz",
    "deal",
    "exact",
    "extra",
    "usage",
)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--write", action="store_true", help="keep the result for the solution page")
    parser.add_argument("--only", nargs="*", help="command ids, for a look at a few")
    args = parser.parse_args()
    scribe = Scribe(os.environ["ELEVENLABS_API_KEY"])
    reader = VoiceReader(os.environ["ANTHROPIC_API_KEY"])
    report = asyncio.run(evaluate(scribe, reader, only=args.only))
    print(json.dumps({k: v for k, v in report.items() if k != "details"}, ensure_ascii=False, indent=1))
    for row in report["details"]:
        if not row["exact"] or row["extra"]:
            print("WRONG", json.dumps(row, ensure_ascii=False))
    if args.write:
        keep = {k: report[k] for k in KEEP}
        RESULT.write_text(json.dumps(keep, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    raise SystemExit(0 if passes(report) else 1)


if __name__ == "__main__":
    main()
