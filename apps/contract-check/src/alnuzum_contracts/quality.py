"""The reference set against the live model: four pairs of a standard and a counterparty's draft (DOCX, PDF, a
text, a scan) with every change known (``samples/truth.json``, made by ``eval/make_samples.py``).

Measures: changes found of those made; false changes (reported, made by nobody); the level of risk matching
the truth; quotes found word for word in the source text of the document (a scan's quotes are of the model's
reading, so they are counted apart); cosmetic changes (wording, requisites) rated high, which must be none.

    uv run python -m alnuzum_contracts.quality            # print
    uv run python -m alnuzum_contracts.quality --write    # and keep quality.json for the page
"""

import argparse
import asyncio
import json
import os
from datetime import UTC, datetime
from decimal import Decimal
from importlib import resources
from pathlib import Path
from typing import Any

from alnuzum_contracts import intake
from alnuzum_contracts.clauses import Comparison, compare, similarity, split, words
from alnuzum_contracts.model import ContractReader
from alnuzum_orders.model import Usage

# what CI holds the live model to (the page shows the measured numbers, not these)
BAR = {"found": 0.95, "false": 2, "risk": 0.75, "quotes": 1.0, "cosmetic_high": 0}


def truth() -> dict[str, Any]:
    data: dict[str, Any] = json.loads(
        resources.files("alnuzum_contracts").joinpath("samples/truth.json").read_text()
    )
    return data


def _file(name: str) -> bytes:
    return resources.files("alnuzum_contracts").joinpath(f"samples/{name}").read_bytes()


def _plain(text: str) -> str:
    return " ".join(text.split())


class Budget:
    """A gate of its own for a run of the set: booked like the page's, never above ``usd``."""

    def __init__(self, usd: Decimal) -> None:
        self.usd, self.booked, self.spent = usd, Decimal(0), Usage()

    async def __call__(self, kind: str, worst: Decimal) -> Any:
        if self.booked + worst > self.usd:
            raise RuntimeError(f"the reference set would spend over ${self.usd}")
        self.booked += worst

        async def done(used: Usage | None, ok: bool) -> None:
            if used is not None:
                self.spent.add(used)
                self.booked -= worst - used.usd

        return done


def _their_index(cmp: Comparison, paragraph: str) -> int | None:
    want = words(paragraph)
    scored = [(similarity(want, words(c.text)), j) for j, c in enumerate(cmp.theirs)]
    best = max(scored, default=(0.0, None))
    return best[1] if best[0] >= 0.5 else None


def score(cmp: Comparison, pair: dict[str, Any], standard: dict[str, Any]) -> dict[str, Any]:
    """The changes found against the edits made."""
    printed = pair["theirs"]
    used: set[int] = set()
    found = risk = cosmetic_high = 0
    missed: list[str] = []
    edits = [e for e in pair["edits"] if e["op"] != "format"]
    for e in edits:
        hit = None
        if e["op"] == "delete":
            i = next((n for n, c in enumerate(cmp.ours) if e["find"] in c.text), None)
            hit = next(
                (
                    c
                    for c in cmp.changes
                    if c.id not in used and c.kind in ("deleted", "changed") and c.a == i
                ),
                None,
            )
        else:
            part = e["with"] if e["op"] == "replace" else e["text"]
            paragraph = next((p for p in printed if part in p), part)
            j = _their_index(cmp, paragraph)
            kinds = ("changed", "added") if e["op"] == "replace" else ("added",)
            hit = next((c for c in cmp.changes if c.id not in used and c.kind in kinds and c.b == j), None)
        if hit is None:
            missed.append(e.get("with") or e.get("text") or e["find"])
            continue
        used.add(hit.id)
        found += 1
        risk += hit.risk == e["risk"]
        cosmetic_high += bool(e.get("cosmetic")) and hit.risk == "high"
    false = [c for c in cmp.changes if c.id not in used]
    source = _plain(" ".join(printed))
    ours_source = _plain(" ".join(standard["text"]))
    quotes = right = 0
    for c in cmp.changes:
        if c.a is not None and c.qa:
            quotes += 1
            right += _plain(cmp.ours[c.a].text[c.qa[0] : c.qa[1]]) in ours_source
        if c.b is not None and c.qb:
            quotes += 1
            right += _plain(cmp.theirs[c.b].text[c.qb[0] : c.qb[1]]) in source
    return {
        "id": pair["id"],
        "changes": len(edits),
        "found": found,
        "false": len(false),
        "false_list": [
            cmp.theirs[c.b].text[:80]
            if c.b is not None
            else cmp.ours[c.a].text[:80]
            if c.a is not None
            else ""
            for c in false
        ],
        "missed": missed,
        "risk": risk,
        "cosmetic_high": cosmetic_high,
        "quotes": quotes,
        "quotes_right": right,
        "risks": [(c.id, c.topic, c.risk, c.rated) for c in cmp.changes],
    }


async def measure(reader: Any, gate: Any) -> dict[str, Any]:
    data = truth()
    rows = []
    for pair in data["pairs"]:
        standard = data["standards"][pair["standard"]]
        ours = split(intake.read(_file(standard["file"])).blocks)
        doc = intake.read(_file(pair["file"]))
        if doc.kind == "scan":
            paragraphs, _ = await reader.read_scan(doc.images, gate)
            doc.blocks = intake.from_paragraphs(paragraphs)
        cmp = compare(ours, split(doc.blocks))
        await reader.rate(cmp, side=standard["side"], lang=standard["lang"], gate=gate)
        row = score(cmp, pair, standard)
        row["scan"] = doc.kind == "scan"
        rows.append(row)
    text_quotes = [r for r in rows if not r["scan"]]
    scan_quotes = [r for r in rows if r["scan"]]
    return {
        "pairs": len(rows),
        "changes": sum(r["changes"] for r in rows),
        "found": sum(r["found"] for r in rows),
        "false": sum(r["false"] for r in rows),
        "risk_match": sum(r["risk"] for r in rows),
        "cosmetic_high": sum(r["cosmetic_high"] for r in rows),
        "quotes": round(
            sum(r["quotes_right"] for r in text_quotes) / max(1, sum(r["quotes"] for r in text_quotes)), 4
        ),
        "quotes_scan": round(
            sum(r["quotes_right"] for r in scan_quotes) / max(1, sum(r["quotes"] for r in scan_quotes)), 4
        ),
        "rows": rows,
    }


def check(result: dict[str, Any]) -> list[str]:
    """What falls below the bar."""
    out = []
    if result["found"] < BAR["found"] * result["changes"]:
        out.append(f"found {result['found']} of {result['changes']}")
    if result["false"] > BAR["false"]:
        out.append(f"false {result['false']}")
    if result["risk_match"] < BAR["risk"] * max(1, result["found"]):
        out.append(f"risk matched {result['risk_match']} of {result['found']}")
    if result["quotes"] < BAR["quotes"]:
        out.append(f"quotes {result['quotes']}")
    if result["cosmetic_high"] > BAR["cosmetic_high"]:
        out.append(f"cosmetic rated high {result['cosmetic_high']}")
    return out


async def run(write: bool) -> dict[str, Any]:
    reader = ContractReader(os.environ["ANTHROPIC_API_KEY"])
    gate = Budget(Decimal("3"))
    result = await measure(reader, gate)
    result["usd"] = str(gate.spent.usd.quantize(Decimal("0.0001")))
    result["calls"] = gate.spent.calls
    result["model"] = reader.model
    result["date"] = datetime.now(UTC).date().isoformat()
    if write:
        await asyncio.to_thread(keep, result)
    return result


def keep(result: dict[str, Any]) -> None:
    """quality.json beside the package: what the page shows."""
    target = Path(str(resources.files("alnuzum_contracts").joinpath("quality.json")))
    target.write_text(json.dumps({k: v for k, v in result.items() if k != "rows"}, indent=1) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args()
    result = asyncio.run(run(args.write))
    for row in result["rows"]:
        print(
            row["id"],
            "found",
            row["found"],
            "/",
            row["changes"],
            "false",
            row["false"],
            "risk",
            row["risk"],
            "quotes",
            row["quotes_right"],
            "/",
            row["quotes"],
        )
        for m in row["missed"]:
            print("   missed:", m)
        for f in row["false_list"]:
            print("   false:", f)
    print(json.dumps({k: v for k, v in result.items() if k != "rows"}, ensure_ascii=False))
    if problems := check(result):
        print("BELOW THE BAR:", "; ".join(problems))


if __name__ == "__main__":
    main()
