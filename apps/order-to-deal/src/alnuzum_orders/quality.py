"""The reference set: six orders in four formats, 67 lines with the right catalog article each.

    ANTHROPIC_API_KEY=… uv run python -m alnuzum_orders.quality [--write]

Metrics:
- accuracy: the system's best guess (its choice, or its first candidate when it chose nothing) is the
  right article; a line not in the catalog counts as right when the system says so;
- flagged: of the wrong guesses, the share the system itself marked for a person (disputed or not found);
- found: order lines the extraction returned; extra: items it returned that are not order lines;
- quantity: of the right guesses, the quantity in the item's unit is right.
The catalog is the demo catalog as a CRM gives it back (description conventions, stock, units).
"""

import argparse
import asyncio
import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from importlib import resources
from pathlib import Path
from typing import Any

from alnuzum_crm.types import Product
from alnuzum_orders import intake
from alnuzum_orders.catalog import Catalog, item_of
from alnuzum_orders.demo_catalog import DEMO_CATALOG
from alnuzum_orders.engine import Line, Result, run
from alnuzum_orders.model import ClaudeReader, Reader, Usage
from alnuzum_orders.text import norm

# the gate of CI: below these the change does not go in
MIN_ACCURACY = 0.9
MIN_FOUND = 0.95
# wrong guesses the system did not mark for a person, as a share of all lines
MAX_SILENT = 0.03
RESULT = Path(__file__).with_name("quality.json")


def demo_catalog() -> Catalog:
    items = tuple(
        item_of(
            Product(
                str(n), i.name, i.sku, i.price_rub, "RUB", "шт" if i.unit == "шт" else None, None, i.description(),
                Decimal(i.stock),
            )
        )
        for n, i in enumerate(DEMO_CATALOG, 1)
    )  # fmt: skip
    return Catalog(items, "RUB", True)


def samples() -> dict[str, bytes]:
    root = resources.files("alnuzum_orders") / "samples"
    truth = json.loads((root / "truth.json").read_text(encoding="utf-8"))
    return {name: (root / name).read_bytes() for name in truth}


def truth() -> dict[str, list[dict[str, Any]]]:
    root = resources.files("alnuzum_orders") / "samples"
    data: dict[str, list[dict[str, Any]]] = json.loads((root / "truth.json").read_text(encoding="utf-8"))
    return data


@dataclass
class Score:
    lines: int = 0
    found: int = 0
    right: int = 0
    wrong: int = 0
    wrong_flagged: int = 0
    qty_right: int = 0
    extra: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "lines": self.lines,
            "found": self.found,
            "accuracy": round(self.right / self.lines, 4) if self.lines else 0,
            "flagged": round(self.wrong_flagged / self.wrong, 4) if self.wrong else 1.0,
            "wrong": self.wrong,
            "wrong_flagged": self.wrong_flagged,
            "quantity": round(self.qty_right / self.right, 4) if self.right else 0,
            "extra": self.extra,
        }


def guess(line: Line) -> tuple[str | None, bool]:
    """(article the system would put in the deal or suggests first, whether a person is asked)."""
    if line.status == "not_found":
        return None, True
    c = line.chosen or (line.candidates[0] if line.candidates else None)
    return (c.sku if c else None), line.status != "ok"


def score(name: str, result: Result, expected: list[dict[str, Any]], into: Score) -> list[dict[str, Any]]:
    used: set[int] = set()
    rows = []
    for want in expected:
        into.lines += 1
        frag = norm(want["fragment"])
        line = next(
            (ln for ln in result.lines if ln.key not in used and frag in norm(ln.text + " " + ln.name)), None
        )
        if line is None:
            into.wrong += 1
            rows.append({"order": name, "fragment": want["fragment"], "got": "missing"})
            continue
        used.add(line.key)
        into.found += 1
        sku, asked = guess(line)
        ok = sku == want["sku"] if want["sku"] else (line.status == "not_found" or line.chosen is None)
        if ok:
            into.right += 1
            if want["sku"] is None or (line.qty_item and Decimal(line.qty_item) == Decimal(str(want["qty"]))):
                into.qty_right += 1
        else:
            into.wrong += 1
            into.wrong_flagged += int(asked)
        rows.append(
            {
                "order": name, "fragment": want["fragment"], "want": want["sku"], "got": sku, "status": line.status,
                "by": line.by, "confidence": line.confidence, "qty": line.qty_item, "ok": ok,
            }
        )  # fmt: skip
    into.extra += len([ln for ln in result.lines if ln.key not in used])
    return rows


async def evaluate(reader: Reader) -> dict[str, Any]:
    catalog = demo_catalog()
    usage = Usage()

    async def gate(kind: str):  # type: ignore[no-untyped-def]
        async def done(used: Usage, ok: bool) -> None:
            usage.add(used)

        return done

    total = Score()
    per_format: dict[str, Score] = {}
    details: list[dict[str, Any]] = []
    expected = truth()
    for name, data in samples().items():
        source = intake.read(name, data)
        result = await run(source, catalog, reader, gate)
        fmt = name.rsplit(".", 1)[1]
        details += score(name, result, expected[name], total)
        score(name, result, expected[name], per_format.setdefault(fmt, Score()))
        if name == "request-en.txt":
            injected = [ln for ln in result.lines if "df333" in norm(ln.text + ln.name)]
            details.append({"order": name, "injection_items": len(injected)})
    return {
        "date": datetime.now(UTC).strftime("%Y-%m-%d"),
        "model": getattr(reader, "model", "?"),
        **total.as_dict(),
        "formats": {k: v.as_dict() for k, v in sorted(per_format.items())},
        "usage": {
            "calls": usage.calls,
            "input": usage.input_tokens,
            "output": usage.output_tokens,
            "usd": str(usage.usd.quantize(Decimal("0.0001"))),
        },
        "details": details,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--write", action="store_true", help="keep the result for the solution page")
    args = parser.parse_args()
    report = asyncio.run(evaluate(ClaudeReader(os.environ["ANTHROPIC_API_KEY"])))
    print(json.dumps({k: v for k, v in report.items() if k != "details"}, ensure_ascii=False, indent=1))
    for row in report["details"]:
        if not row.get("ok", True):
            print("WRONG", json.dumps(row, ensure_ascii=False))
    if args.write:
        keep = {
            k: report[k]
            for k in (
                "date",
                "model",
                "lines",
                "accuracy",
                "flagged",
                "wrong",
                "wrong_flagged",
                "found",
                "quantity",
                "usage",
            )
        }
        keep["orders"] = len(truth())
        RESULT.write_text(json.dumps(keep, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    raise SystemExit(0 if passes(report) else 1)


def passes(report: dict[str, Any]) -> bool:
    silent = (report["wrong"] - report["wrong_flagged"]) / report["lines"]
    return bool(
        report["accuracy"] >= MIN_ACCURACY
        and silent <= MAX_SILENT
        and report["found"] >= MIN_FOUND * report["lines"]
    )


if __name__ == "__main__":
    main()
