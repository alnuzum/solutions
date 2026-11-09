"""Live: the page's own engine over the demo history of the shared test accounts (the same accounts the demo of
production reads). Who missed their cycle must be the plan's answer, and the others on their cycle or short of
history, so a demo that grew old (its daily top-up stopped) turns this red. Odoo is left out: the Odoo of CI is
a fresh one, the demo's lives on the server. Reads only."""

import os
from datetime import UTC, datetime, timedelta

import pytest

from alnuzum_crm.amocrm import AmoCRM, Kommo
from alnuzum_crm.base import CRM
from alnuzum_crm.bitrix24 import Bitrix24
from alnuzum_crm.oauth import zoho_refresh
from alnuzum_crm.tokens import MemoryOAuthToken, StaticToken, TokenSet
from alnuzum_crm.types import CompanyIn
from alnuzum_crm.zoho import ZohoCRM
from alnuzum_winback import demo as plan
from alnuzum_winback.rule import today
from alnuzum_winback.scan import analyse, name_clients, only_demo, read_account

pytestmark = pytest.mark.e2e

KINDS = {"Ромашка": "missed", "Samarqand Tekstil": "missed", "Navruz Stroy": "on_cycle"}
KINDS |= {"Bahor Market": "on_cycle", "Tashkent City Mall": "short"}


def need(*names: str) -> dict[str, str]:
    missing = [n for n in names if not os.environ.get(n)]
    if missing:
        if os.environ.get("E2E_REQUIRE_ALL") == "1":
            pytest.fail(f"missing secrets: {', '.join(missing)}")
        pytest.skip(f"no {', '.join(missing)}")
    return {n: os.environ[n] for n in names}


def adapter(kind: str) -> CRM:
    if kind in ("amocrm", "kommo"):
        prefix, cls = ("AMOCRM", AmoCRM) if kind == "amocrm" else ("KOMMO", Kommo)
        env = need(f"{prefix}_DOMAIN", f"{prefix}_TOKEN")
        return cls(env[f"{prefix}_DOMAIN"], StaticToken(env[f"{prefix}_TOKEN"], crm=kind))
    if kind == "bitrix24":
        return Bitrix24(need("B24_WEBHOOK")["B24_WEBHOOK"])
    env = need("ZOHO_CLIENT_ID", "ZOHO_CLIENT_SECRET", "ZOHO_REFRESH_TOKEN")
    accounts = os.environ.get("ZOHO_ACCOUNTS_SERVER", "https://accounts.zoho.com")

    async def refresh(tokens: TokenSet) -> TokenSet:
        return await zoho_refresh(accounts, env["ZOHO_CLIENT_ID"], env["ZOHO_CLIENT_SECRET"], tokens)

    expired = TokenSet("", env["ZOHO_REFRESH_TOKEN"], datetime.now(UTC) - timedelta(seconds=1))
    return ZohoCRM(
        os.environ.get("ZOHO_API_DOMAIN", "https://developer.zohoapis.com"),
        MemoryOAuthToken(expired, refresh),
    )


@pytest.mark.parametrize("kind", ["amocrm", "kommo", "bitrix24", "zoho"])
async def test_the_demo_gives_the_plans_answer(kind: str, capsys: pytest.CaptureFixture[str]) -> None:
    day = today(datetime.now(UTC))
    async with adapter(kind) as crm:
        present = {b.company for b in plan.BUYERS if await crm.find_company(CompanyIn(b.company))}
        read = await read_account(crm, kind, day, cap=20_000, seconds=120)
        found = await name_clients(crm, analyse(only_demo(read.purchases), day), demo=True)
    got = {c["name"]: c["kind"] for c in [*found["clients"], *found["others"]]}
    with capsys.disabled():
        print(f"\n{kind}: {read.cut or 'whole'} read, {len(read.purchases)} won deals, {got}")
    assert read.cut is None
    assert present, f"{kind}: no demo company in the account"
    assert got == {name: KINDS[name] for name in present}, f"{kind}: the demo's answer moved"
    assert {c["name"] for c in found["clients"]} == plan.expected(present)
