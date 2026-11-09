"""Load the demo catalog of the order-to-deal solution into the demo account of a CRM.

    uv run python scripts/load_demo_catalog.py amocrm kommo bitrix24 zoho odoo

Credentials come from the same environment variables as the live E2E tests. Items already in the
catalog (by article) are left alone, so the script can be run again after a partial run.
"""

import argparse
import asyncio
import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from alnuzum_crm.amocrm import AmoCRM, Kommo
from alnuzum_crm.base import CRM
from alnuzum_crm.bitrix24 import Bitrix24
from alnuzum_crm.oauth import zoho_refresh
from alnuzum_crm.odoo import Odoo
from alnuzum_crm.tokens import MemoryOAuthToken, StaticToken, TokenSet
from alnuzum_crm.types import ProductIn
from alnuzum_crm.zoho import ZohoCRM
from alnuzum_orders.demo_catalog import DEMO_CATALOG, PREFIX, price_in

# the currency each demo account keeps its prices in (read from the accounts on 09.11.2026)
CURRENCY = {"amocrm": "UZS", "kommo": "UZS", "bitrix24": "RUB", "zoho": "UZS", "odoo": "USD"}


def adapter(name: str) -> CRM:
    env = os.environ
    if name in ("amocrm", "kommo"):
        cls = Kommo if name == "kommo" else AmoCRM
        prefix = name.upper()
        return cls(env[f"{prefix}_DOMAIN"], StaticToken(env[f"{prefix}_TOKEN"], crm=name))
    if name == "bitrix24":
        return Bitrix24(env["B24_WEBHOOK"])
    if name == "zoho":
        accounts = env.get("ZOHO_ACCOUNTS_SERVER", "https://accounts.zoho.com")

        async def refresh(tokens: TokenSet) -> TokenSet:
            return await zoho_refresh(accounts, env["ZOHO_CLIENT_ID"], env["ZOHO_CLIENT_SECRET"], tokens)

        expired = TokenSet("", env["ZOHO_REFRESH_TOKEN"], datetime.now(UTC) - timedelta(seconds=1))
        return ZohoCRM(
            env.get("ZOHO_API_DOMAIN", "https://developer.zohoapis.com"), MemoryOAuthToken(expired, refresh)
        )
    return Odoo(env["ODOO_URL"], env["ODOO_DB"], env["ODOO_LOGIN"], env["ODOO_PASSWORD"], trusted_url=True)


async def existing(crm: CRM) -> set[str]:
    skus: set[str] = set()
    cursor: str | None = None
    while True:
        page = await crm.list_products(cursor=cursor, limit=200)
        skus |= {p.sku for p in page.items if p.sku and p.sku.startswith(PREFIX)}
        if page.next_cursor is None:
            return skus
        cursor = page.next_cursor


async def load(name: str) -> None:
    currency = CURRENCY[name]
    async with adapter(name) as crm:
        have = await existing(crm)
        todo = [i for i in DEMO_CATALOG if i.sku not in have]
        print(f"{name}: {len(have)} in the catalog, {len(todo)} to add")
        for n, item in enumerate(todo, 1):
            await crm.create_product(
                ProductIn(
                    item.name,
                    price_in(item, currency),
                    item.sku,
                    unit=item.unit,
                    category=item.category,
                    description=item.description(),
                    stock=Decimal(item.stock),
                )
            )
            if n % 25 == 0:
                print(f"{name}: {n} added")
        print(f"{name}: done, {len(have) + len(todo)} demo items")


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("crm", nargs="+", choices=sorted(CURRENCY))
    args = parser.parse_args()
    await asyncio.gather(*(load(name) for name in args.crm))


if __name__ == "__main__":
    asyncio.run(main())
