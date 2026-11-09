"""Load the demo deals of the voice solution into the demo account of a CRM.

    uv run python scripts/load_voice_demo.py amocrm bitrix24 zoho odoo
    uv run python scripts/load_voice_demo.py kommo --deals D1 D2

The deals are the five of the reference set (``alnuzum_voice/samples/truth.json``): a company, its
contact and a deal titled «Демо · …», the only deals the demo searches. Credentials come from the same
environment variables as the live E2E tests. A deal already there (by its exact title) is left alone,
so the script can be run again. Kommo's trial account holds 100 deals in all: give it two.
"""

import argparse
import asyncio
import json
from pathlib import Path

from load_demo_catalog import CURRENCY, adapter

from alnuzum_crm.types import CompanyIn, ContactIn, DealIn

TRUTH = Path(__file__).parent.parent / "apps/voice-to-crm/src/alnuzum_voice/samples/truth.json"


def deals() -> dict[str, dict[str, str]]:
    return json.loads(TRUTH.read_text(encoding="utf-8"))["deals"]  # type: ignore[no-any-return]


async def load(name: str, only: list[str] | None) -> None:
    async with adapter(name) as crm:
        for key, d in deals().items():
            if only and key not in only:
                continue
            if any(hit.title == d["title"] for hit in await crm.find_deals(d["title"], limit=20)):
                print(f"{name}: {key} is there")
                continue
            company = await crm.find_or_create_company(CompanyIn(d["company"]))
            contact = await crm.find_or_create_contact(ContactIn(d["contact"]), company_id=company.id)
            made = await crm.create_deal(
                DealIn(
                    d["title"],
                    company_id=company.id,
                    contact_id=contact.id,
                    currency=None if name == "odoo" else CURRENCY[name],
                )
            )
            print(f"{name}: {key} created as {made.id}")


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("crm", nargs="+", choices=sorted(CURRENCY))
    parser.add_argument("--deals", nargs="*", help="keys of truth.json, e.g. D1 D2 (default: all five)")
    args = parser.parse_args()
    await asyncio.gather(*(load(name, args.deals) for name in args.crm))


if __name__ == "__main__":
    asyncio.run(main())
