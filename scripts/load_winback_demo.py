"""Load the purchase history of the win-back solution into the demo account of a CRM.

    uv run python scripts/load_winback_demo.py amocrm kommo bitrix24 zoho odoo
    uv run python scripts/load_winback_demo.py --check amocrm kommo bitrix24 zoho odoo

The buyers are the «Демо · …» companies of the voice solution (``load_voice_demo.py``); a company the
account does not have is skipped, never created (amoCRM and Kommo keep 100 contacts and companies in all).
Each purchase is a won deal «Демо · Покупка · <company> · <date>» with products of the demo catalog at the
account's catalog price, closed on its date. Two companies stopped buying a few cycles ago, two still buy on
their cycle, one has too short a history to judge. The plan is fixed to calendar dates: a run makes only the
purchases whose date has come and that are not there yet, so running it again later tops the history up and
the demo does not grow old.

amoCRM and Kommo: the open deals and the contacts and companies are counted before the first purchase and
after it; the run stops if either count moved (a technical account keeps 100 open deals). ``--check`` reads
the account with the page's own engine (``alnuzum_winback.scan``), all won deals of 24 months page by page,
and prints who missed their cycle; it fails when that is not the plan's answer (Zoho's search finds a deal
minutes after it is written). The plan itself lives in ``alnuzum_winback.demo``.
"""

import argparse
import asyncio
import sys
from datetime import UTC, date, datetime

from load_demo_catalog import CURRENCY, adapter

from alnuzum_crm.types import CompanyIn
from alnuzum_winback import demo as plan
from alnuzum_winback.rule import today as local_today
from alnuzum_winback.scan import analyse, name_clients, only_demo, read_account


async def load(name: str, today: date) -> None:
    async with adapter(name) as crm:
        try:
            made = await plan.seed(crm, name, today)
        except plan.CountsMoved as exc:
            sys.exit(f"counts moved after the first purchase: {exc}, stopped")
        print(f"{name}: {made} purchases added")


async def check(name: str, today: date) -> bool:
    async with adapter(name) as crm:
        present = {b.company for b in plan.BUYERS if await crm.find_company(CompanyIn(b.company))}
        read = await read_account(crm, name, today, cap=20_000, seconds=300)
        found = await name_clients(crm, analyse(only_demo(read.purchases), today), demo=True)
    for row in [*found["clients"], *found["others"]]:
        print(
            f"{name}: {row['name']}: {row['orders']} orders, {row['kind']}, cycle {row['cycle']}, "
            f"{row['since']} days since the last"
        )
    missed = {c["name"] for c in found["clients"]}
    expected = plan.expected(present)
    ok = missed == expected and read.cut is None
    result = "ok" if ok else "WRONG"
    print(f"{name}: missed their cycle {sorted(missed)}, expected {sorted(expected)}: {result}")
    return ok


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("crm", nargs="+", choices=sorted(CURRENCY))
    parser.add_argument(
        "--check", action="store_true", help="read the account and print who missed the cycle"
    )
    args = parser.parse_args()
    today = local_today(datetime.now(UTC))
    if args.check:
        results = await asyncio.gather(*(check(name, today) for name in args.crm))
        if not all(results):
            sys.exit(1)
    else:
        await asyncio.gather(*(load(name, today) for name in args.crm))


if __name__ == "__main__":
    asyncio.run(main())
